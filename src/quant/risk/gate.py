"""Single entry point for every order intent. Only approved intents reach the queue.

Flow stage: Trade intent, Risk gate
Status: PHASE 1 IMPLEMENTED
Existing code to reuse or wrap (do not copy): Blue_engine/core/risk_gate.py (values via risk/blue_risk_gate.py)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Set

from quant.core.config import RiskLimits
from quant.core.types import GateDecision, TradeIntent
from quant.monitor.safe_mode import Mode, SafeMode
from quant.risk.blue_risk_gate import entry_breaches
from quant.risk.controls import in_event_blackout
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
    # Symbol -> dates when new entries are blocked (from event_blackout_dates).
    blackout_dates: Dict[str, Set[date]] = field(default_factory=dict)


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
    if is_entry:
        # Fail closed: no trade date while a blackout exists means we cannot check it.
        blocked = state.blackout_dates.get(intent.symbol, set())
        if intent.trade_day is None:
            if blocked:
                reasons.append(f"event blackout unchecked for {intent.symbol}: trade_day missing")
        elif in_event_blackout(intent.trade_day, blocked):
            reasons.append(f"event blackout on {intent.trade_day.isoformat()} for {intent.symbol}")

    reasons += pretrade_checks(intent, limits)
    reasons += price_band_checks(intent)
    reasons += liquidity_checks(intent, limits)
    if is_entry and limits.max_open_exposure is None:
        reasons.append("limit not set: max_open_exposure")
    # Monetary values (order qty, notional, daily loss, open exposure) are checked by Blue_engine.
    reasons += entry_breaches(
        intent, limits, open_notional=state.open_notional, daily_pnl=state.daily_pnl
    )
    reasons += loss_checks(
        intent, state.daily_pnl, state.strategy_pnl.get(intent.strategy_id, 0.0), limits
    )
    return GateDecision(intent.intent_id, not reasons, tuple(reasons))
