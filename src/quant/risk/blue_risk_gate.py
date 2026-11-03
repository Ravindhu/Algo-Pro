"""Monetary and size breach arithmetic for ENTRY intents, delegated to Blue_engine's RiskGate.

Flow stage: Risk gate (monetary breach checks)
Status: PHASE 1 IMPLEMENTED (Phase 0 wrap: monetary arithmetic delegated to Blue_engine)
Existing code to reuse or wrap (do not copy): Blue_engine/core/risk_gate.py

WHAT THIS FILE DOES
-------------------
Evaluates the configured limits for max order quantity, max order notional, max daily
loss and max open exposure. Blue's RiskGate does the arithmetic and fails closed when a
value cannot be checked (for example, a missing price).

WHAT STAYS LOCAL (on purpose)
-----------------------------
- The "limit not set" policy. Blue treats a missing limit as "not configured" and approves.
  This project treats a missing entry limit as a rejection. That policy is enforced in the
  local modules (pretrade.py, loss_breaker.py, gate.py) before this adapter runs.
- Blue's live acknowledgement is not used. The src gate has no live mode yet.
- Exits never reach Blue. Exits reduce risk, so entry caps do not apply to them.
"""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any, Dict, List

from quant.core.config import RiskLimits
from quant.core.types import TradeIntent

_log = logging.getLogger("quant.risk.blue_gate")


@lru_cache(maxsize=1)
def _gate_class() -> Any:
    """Resolve Blue_engine's RiskGate once. Raises BlueEngineUnavailable if it cannot load."""
    from quant.core.blue_bridge import load
    return load("core.risk_gate").RiskGate


def _config_from(limits: RiskLimits) -> Dict[str, Any]:
    """Only the limits that are set. Blue reads a missing key as 'not configured'."""
    pairs = {
        "max_order_quantity": limits.max_order_quantity,
        "max_order_notional": limits.max_order_notional,
        "max_daily_loss": limits.max_daily_loss,
        "max_open_exposure": limits.max_open_exposure,
    }
    return {key: value for key, value in pairs.items() if value is not None}


def entry_breaches(
    intent: TradeIntent,
    limits: RiskLimits,
    *,
    open_notional: float,
    daily_pnl: float,
) -> List[str]:
    """Breach reasons for an ENTRY intent under the configured monetary limits.

    Returns [] for exits and protection orders, and when no monetary limit is set here.
    """
    if intent.kind != "entry":
        return []
    config = _config_from(limits)
    if not config:
        return []
    gate = _gate_class()(config, log=_log.debug)
    decision = gate.check(
        {"action": intent.side, "quantity": intent.qty, "price": intent.ref_price},
        day_pnl=daily_pnl,
        open_exposure=open_notional,
        require_intent_price=True,
    )
    return list(decision.breaches)
