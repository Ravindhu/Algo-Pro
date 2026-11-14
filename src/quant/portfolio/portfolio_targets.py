"""Portfolio engine wrapper: target weights and rebalance advice, DELEGATED to Blue_engine.

Flow stage: Portfolio engine
Status: PHASE 5 IMPLEMENTED (wrap; advice only, long-only, persistence forced OFF)
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/portfolio/portfolio_engine.py
(PortfolioEngine)

Rules enforced here, not only in docs:
- Persistence is forced OFF. Blue's portfolio state write is never called from here.
- ``rebalance_trades`` and hedge notes are ADVICE. Nothing in this module sends an order.
  Any trade must still pass the risk gate.
- Long-only: a negative target weight fails the whole result.

FAIL-CLOSED RULES
-----------------
- Blue unavailable, the engine raises, or a weight is negative or not a number: ``ok=False``.
- Never raises.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional

from quant.core.blue_bridge import BlueEngineUnavailable, load

__all__ = ["run_portfolio"]


def run_portfolio(config: Optional[Dict[str, Any]] = None,
                  portfolio_inputs: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return ``{ok, target_weights, rebalance_trades, advice_only, warnings, reason}``."""
    failed = {"ok": False, "target_weights": {}, "rebalance_trades": [],
              "advice_only": True, "warnings": []}
    try:
        cls = load("portfolio.portfolio_engine").PortfolioEngine
    except BlueEngineUnavailable as exc:
        return {**failed, "reason": f"Blue portfolio engine unavailable: {exc}"}
    cfg = dict(config or {})
    cfg["persist"] = False  # never write portfolio state from the quant layer
    if portfolio_inputs is not None:
        cfg["portfolio_inputs"] = dict(portfolio_inputs)
    try:
        result = cls(cfg).run({})
    except Exception as exc:  # noqa: BLE001
        return {**failed, "reason": f"portfolio engine failed: {exc!r}"}
    data = getattr(result, "data", None) or {}
    weights = data.get("target_weights") or {}
    if not isinstance(weights, dict):
        return {**failed, "reason": "target weights are not a mapping"}
    for name, w in weights.items():
        try:
            value = float(w)
        except (TypeError, ValueError):
            return {**failed, "reason": f"weight for {name} is not a number"}
        if not math.isfinite(value) or value < 0:
            return {**failed, "reason": f"weight for {name} is negative or not finite (long-only)"}
    return {
        "ok": bool(getattr(result, "ok", False)),
        "target_weights": {k: float(v) for k, v in weights.items()},
        "rebalance_trades": list(data.get("rebalance_trades") or []),
        "advice_only": True,
        "warnings": list(getattr(result, "warnings", []) or []),
        "reason": str(getattr(result, "error", None) or ""),
    }
