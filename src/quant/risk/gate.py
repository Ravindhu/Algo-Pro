"""Single entry point for every order intent. Only approved intents reach the queue.

Flow stage: Trade intent, Risk gate
Status: PHASE 1 IMPLEMENTED
Existing code to reuse or wrap (do not copy): Blue_engine/core/risk_gate.py (ported to risk/money_checks.py; no Blue import)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Set

from quant.core.config import RiskLimits
from quant.core.types import GateDecision, TradeIntent
from quant.monitor.safe_mode import Mode, SafeMode
from quant.risk.money_checks import entry_breaches
from quant.risk.controls import HaltHandler, in_event_blackout
from quant.risk.kill_close import KILL_STRATEGY_ID
from quant.risk.kill_switch import KillSwitch
from quant.risk.liquidity import liquidity_checks
from quant.risk.loss_breaker import loss_checks
from quant.risk.pretrade import pretrade_checks
from quant.risk.price_band import price_band_checks
from quant.risk.symbol_cap import symbol_cap_checks


@dataclass
class RiskState:
    open_notional: float = 0.0
    daily_pnl: float = 0.0
    strategy_pnl: dict = field(default_factory=dict)
    seen_ids: Set[str] = field(default_factory=set)
    # Symbol -> dates when new entries are blocked (from event_blackout_dates).
    blackout_dates: Dict[str, Set[date]] = field(default_factory=dict)
    # Symbols with a non-zero net position in the ledger (for max_symbols_per_portfolio).
    open_symbols: Set[str] = field(default_factory=set)


def check_intent(
    intent: TradeIntent,
    state: RiskState,
    limits: RiskLimits,
    kill: KillSwitch,
    safe: SafeMode,
    available_funds: Optional[float] = None,
    halt: Optional[HaltHandler] = None,
    held_qty: Optional[int] = None,
) -> GateDecision:
    reasons: List[str] = []
    is_entry = intent.kind == "entry"
    if is_entry and halt is not None and halt.frozen:
        reasons.append(f"index circuit freeze active: {sorted(halt.frozen)}")
    if is_entry and available_funds is not None and intent.notional > available_funds:
        reasons.append(f"insufficient funds: need {intent.notional:.2f}, available {available_funds:.2f}")
    # Kill closes pass halt-all, but only while the kill switch is on (decision 0020).
    is_kill_close = kill.active and intent.kind == "exit" and intent.strategy_id == KILL_STRATEGY_ID
    # Risk-reducing exit (owner decision 2026-11-08): a SELL exit closes a long position, so it lowers risk
    # and passes HALT_ALL like a kill close. The book is long-only, so a SELL exit never opens a short.
    # Held quantity: when the caller passes held_qty, a SELL exit may not be larger than what is held.
    # The gate cannot see the broker book, so callers must pass held_qty (the bot does). Entries and
    # BUY exits stay blocked under HALT_ALL.
    is_risk_reducing_exit = intent.kind == "exit" and intent.side == "SELL"
    if is_risk_reducing_exit and not is_kill_close and held_qty is not None and intent.qty > max(held_qty, 0):
        reasons.append(f"exit qty {intent.qty} exceeds held qty {held_qty}; a SELL exit must not open a short")

    if limits.blocked:
        reasons.append("risk_limits.blocked is set")
    if kill.active and is_entry:
        reasons.append(f"kill switch: {kill.reason}")
    if safe.mode is Mode.HALT_ALL and not (is_kill_close or is_risk_reducing_exit):
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

    if is_entry:
        reasons += symbol_cap_checks(intent, state.open_symbols, limits)
    reasons += pretrade_checks(intent, limits)
    reasons += price_band_checks(intent)
    reasons += liquidity_checks(intent, limits)
    if is_entry and limits.max_open_exposure is None:
        reasons.append("limit not set: max_open_exposure")
    # Monetary values (order qty, notional, daily loss, open exposure) are checked in money_checks.py.
    reasons += entry_breaches(
        intent, limits, open_notional=state.open_notional, daily_pnl=state.daily_pnl
    )
    reasons += loss_checks(
        intent, state.daily_pnl, state.strategy_pnl.get(intent.strategy_id, 0.0), limits
    )
    return GateDecision(intent.intent_id, not reasons, tuple(reasons))
