"""Dashboard data: P&L, win rate, profit factor, and risk use.

Owner rule (AGENTS rule 8, decision 0008, 2026-11-05):
- P&L is SHOWN by default (decision 0007).
- EV, MAE/MFE and rule compliance are REMOVED from the dashboard.
- MAE/MFE are not lost: they feed the optimizer objective as parameters
  (see Blue_engine/optimizer/objective.py and engine.py EngineConfig).

Flow stage: Dashboard
Status: PHASE 3 IMPLEMENTED (profit factor delegated to Blue backtest.metrics; rest local)
Existing code to reuse or wrap (do not copy): Blue_engine/backtest/metrics.py (the Blue terminal console was removed 2026-11-05)
"""
from __future__ import annotations

from typing import Dict, Optional, Sequence

import numpy as np

from quant.core.blue_bridge import load


def profit_factor(gross_profit: float, gross_loss: float) -> Optional[float]:
    """Blue backtest.metrics.profit_factor: None when there are no losses."""
    return load("backtest.metrics").profit_factor(gross_profit, gross_loss)


def build_payload(trades: Sequence[dict], risk_used: float) -> Dict[str, object]:
    """trades: dicts with ret (fraction) and pnl (money). Extra keys are ignored."""
    if not trades:
        return {"trades": 0, "total_pnl": 0.0, "win_rate": None,
                "profit_factor": None, "risk_utilisation": risk_used}
    ret = np.asarray([t["ret"] for t in trades], float)
    pnl = np.asarray([t["pnl"] for t in trades], float)
    wins, losses = pnl[pnl > 0].sum(), pnl[pnl < 0].sum()
    pf: Optional[float] = profit_factor(float(wins), float(losses))
    return {
        "trades": len(trades),
        "total_pnl": float(pnl.sum()),
        "win_rate": float((ret > 0).mean()),
        "profit_factor": pf,
        "risk_utilisation": risk_used,
    }
