"""Dashboard data: EV, win rate, profit factor, MAE/MFE, risk use, rule compliance. P&L hidden by default.

Flow stage: Dashboard
Status: PHASE 3 IMPLEMENTED
Existing code to reuse or wrap (do not copy): Blue_engine/dashboard/app.py
"""
from __future__ import annotations

from typing import Dict, Optional, Sequence

import numpy as np


def build_payload(trades: Sequence[dict], risk_used: float, rule_breaches: int,
                  reveal_pnl: bool = False) -> Dict[str, object]:
    """trades: dicts with ret (fraction), pnl (money), mae and mfe (fractions).

    Percent metrics always show. Money (pnl) shows only when reveal_pnl is True.
    """
    if not trades:
        return {"trades": 0, "ev_pct_per_trade": None, "win_rate": None, "profit_factor": None,
                "mean_mae": None, "mean_mfe": None, "risk_utilisation": risk_used,
                "rule_breaches": rule_breaches, "pnl_hidden": not reveal_pnl}
    ret = np.asarray([t["ret"] for t in trades], float)
    pnl = np.asarray([t["pnl"] for t in trades], float)
    wins, losses = pnl[pnl > 0].sum(), pnl[pnl < 0].sum()
    pf: Optional[float] = float(wins / abs(losses)) if losses < 0 else None
    payload: Dict[str, object] = {
        "trades": len(trades),
        "ev_pct_per_trade": float(ret.mean() * 100),
        "win_rate": float((ret > 0).mean()),
        "profit_factor": pf,
        "mean_mae": float(np.mean([t["mae"] for t in trades])),
        "mean_mfe": float(np.mean([t["mfe"] for t in trades])),
        "risk_utilisation": risk_used,
        "rule_breaches": rule_breaches,
        "pnl_hidden": not reveal_pnl,
    }
    if reveal_pnl:
        payload["total_pnl"] = float(pnl.sum())
    return payload
