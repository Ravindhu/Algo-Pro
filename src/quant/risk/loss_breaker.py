"""Daily and per-strategy loss budgets. Breach blocks new entries only.

Flow stage: Loss budget breaker
Status: PHASE 1 IMPLEMENTED
Existing code to reuse or wrap (do not copy): Blue_engine/core/risk_gate.py (daily value via risk/blue_risk_gate.py)
"""
from __future__ import annotations

from typing import List

from quant.core.config import RiskLimits
from quant.core.types import TradeIntent


def loss_checks(intent: TradeIntent, daily_pnl: float, strategy_pnl: float, limits: RiskLimits) -> List[str]:
    """Policy for daily and strategy loss budgets. The daily VALUE check is in risk/blue_risk_gate.py."""
    if intent.kind != "entry":
        return []
    reasons: List[str] = []
    if limits.max_daily_loss is None:
        reasons.append("limit not set: max_daily_loss")
    if limits.max_strategy_loss is None:
        reasons.append("limit not set: max_strategy_loss")
    elif strategy_pnl <= -limits.max_strategy_loss:
        reasons.append(f"strategy {intent.strategy_id} loss budget breached")
    return reasons
