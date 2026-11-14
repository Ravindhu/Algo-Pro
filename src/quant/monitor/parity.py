"""Live vs backtest parity: compares live fill prices with the replayed backtest fill prices.

Flow stage: Live vs backtest parity
Status: PHASE 2 IMPLEMENTED
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/core/paper_shift.py
"""
from __future__ import annotations

from typing import Dict


def parity_report(live: Dict[str, float], replay: Dict[str, float], tol_bps: float = 10.0) -> Dict[str, object]:
    common = sorted(set(live) & set(replay))
    if not common:
        return {"matched": 0, "mean_abs_bps": None, "passed": False}
    bps = [abs(live[k] - replay[k]) / replay[k] * 1e4 for k in common]
    mean_bps = sum(bps) / len(bps)
    return {"matched": len(common), "mean_abs_bps": mean_bps, "passed": mean_bps <= tol_bps}
