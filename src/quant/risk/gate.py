"""Single entry point for every order intent. Only approved intents reach the queue.

Flow stage: Trade intent, Risk gate
Status: PHASE 1 IMPLEMENTED
Existing code to reuse or wrap (do not copy): Blue_engine/core/risk_gate.py
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Set

from quant.core.config import RiskLimits
from quant.core.types import GateDecision, TradeIntent
from quant.monitor.safe_mode import Mode, SafeMode
from quant.risk.kill_switch import KillSwitch
from quant.risk.liquidity import liquidity_checks
from quant.risk.loss_breaker import loss_checks
from quant.risk.pretrade import pretrade_checks
from quant.risk.price_band import price_band_checks


@dataclass
class RiskState:
    open_notional: float = 0.0
    daily_pnl: float = 0.0
    strategy_pnl: dict = field(default_factory=dict)
    seen_ids: Set[str] = field(default_factory=set)


def check_intent(
    intent: TradeIntent,
    state: RiskState,
    limits: RiskLimits,
    kill: KillSwitch,
    safe: SafeMode,
) -> GateDecision:
    reasons: List[str] = []
    is_entry = intent.kind == "entry"

    if limits.blocked:
        reasons.append("risk_limits.blocked is set")
    if kill.active and is_entry:
        reasons.append(f"kill switch: {kill.reason}")
    if safe.mode is Mode.HALT_ALL:
        reasons.append("safe mode: halt-all")
    if safe.mode is Mode.REDUCE_ONLY and is_entry:
        reasons.append("safe mode: reduce-only")
    if intent.intent_id in state.seen_ids:
        reasons.append("duplicate intent id")

    reasons += pretrade_checks(intent, limits)
    reasons += price_band_checks(intent)
    reasons += liquidity_checks(intent, limits)
    if is_entry:
        if limits.max_open_exposure is None:
            reasons.append("limit not set: max_open_exposure")
        elif state.open_notional + intent.notional > limits.max_open_exposure:
            reasons.append("open exposure limit would be exceeded")
    reasons += loss_checks(
        intent, state.daily_pnl, state.strategy_pnl.get(intent.strategy_id, 0.0), limits
    )
    return GateDecision(intent.intent_id, not reasons, tuple(reasons))
