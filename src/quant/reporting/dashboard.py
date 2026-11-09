"""Dashboard data: P&L, win rate, profit factor, and risk use.

Owner rule (AGENTS rule 8, decision 0008, 2026-11-05):
- P&L is SHOWN by default (decision 0007).
- EV is SHOWN (owner decision 2026-11-09; the AGENTS rule 8 EV ban was lifted).
- MAE/MFE and rule compliance are still REMOVED from the dashboard.
- MAE/MFE are not lost: they feed the optimizer objective as parameters
  (see Blue_engine/optimizer/objective.py and engine.py EngineConfig).

Flow stage: Dashboard
Status: PHASE 3 IMPLEMENTED (stage 3 port 2026-11-06: profit factor is local; no Blue import)
Ported from: Blue_engine/backtest/metrics.py (profit_factor). Parity: tests/parity/test_stage3_parity.py
"""
from __future__ import annotations

from typing import Dict, Optional, Sequence

import numpy as np



def profit_factor(gross_profit: float, gross_loss: float) -> Optional[float]:
    """Total winning rupees / total losing rupees. None when there are no losses."""
    if gross_loss == 0:
        return None
    return gross_profit / abs(gross_loss)


def expectancy(trades: Sequence[dict]) -> Dict[str, Optional[float]]:
    """Expected value (EV) per closed trade, plus average win, average loss and risk-to-reward.

    ev_per_trade = mean pnl (money). It equals win_rate * avg_win + (1 - win_rate) * avg_loss.
    ev_pct_per_trade = mean ret (fraction). Any value is None when it cannot be computed.
    """
    if not trades:
        return {"ev_per_trade": None, "ev_pct_per_trade": None,
                "avg_win": None, "avg_loss": None, "risk_reward": None}
    ret = np.asarray([t["ret"] for t in trades], float)
    pnl = np.asarray([t["pnl"] for t in trades], float)
    wins, losses = pnl[pnl > 0], pnl[pnl < 0]
    avg_win = float(wins.mean()) if wins.size else None
    avg_loss = float(losses.mean()) if losses.size else None
    rr = avg_win / abs(avg_loss) if avg_win is not None and avg_loss else None
    return {"ev_per_trade": float(pnl.mean()), "ev_pct_per_trade": float(ret.mean()),
            "avg_win": avg_win, "avg_loss": avg_loss, "risk_reward": rr}


def build_payload(trades: Sequence[dict], risk_used: float) -> Dict[str, object]:
    """trades: dicts with ret (fraction) and pnl (money). Extra keys are ignored."""
    if not trades:
        return {"trades": 0, "total_pnl": 0.0, "win_rate": None,
                "profit_factor": None, "risk_utilisation": risk_used, **expectancy([])}
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
        **expectancy(trades),
    }
