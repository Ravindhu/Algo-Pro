"""Strategy capital budgets, capacity caps, margin and T+1 funds check.

Flow stage: Capital allocator, Cash and margin manager
Status: PHASE 2 IMPLEMENTED
Existing code to reuse or wrap (do not copy): Blue_engine/core/risk_context.py
"""
from __future__ import annotations

from typing import Dict


def allocate(budget: float, scores: Dict[str, float], capacity: Dict[str, float]) -> Dict[str, float]:
    """Split budget by positive score, never above each strategy's capacity. Unused budget stays unallocated."""
    pos = {k: max(0.0, v) for k, v in scores.items()}
    total = sum(pos.values())
    if total <= 0 or budget <= 0:
        return {k: 0.0 for k in scores}
    out = {}
    for k in scores:
        raw = budget * pos[k] / total
        out[k] = min(raw, capacity.get(k, 0.0))
    return out


def margin_ok(notional: float, margin_pct: float, available_margin: float) -> bool:
    return notional * margin_pct <= available_margin


def funds_available_t1(settled_cash: float, sell_proceeds_t1: float, reserved: float = 0.0) -> float:
    """Cash usable today: settled cash plus sale proceeds that settle tomorrow, minus reserves."""
    return max(0.0, settled_cash + sell_proceeds_t1 - reserved)
