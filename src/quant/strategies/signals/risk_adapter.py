"""Flow stage: Trade intent (signal -> live contract -> Risk gate).
Status: PHASE 2 IMPLEMENTED (ported or new, with tests)
Tests: src/quant/tests/test_signals_risk_adapter.py.

What this does, in order:
  1. Reads the strategy signal (side, entry, sl_dist, tp_dist).
  2. Sets the stop-loss and target prices on the correct side of entry.
  3. Sizes the quantity from equity and risk-per-trade (qty = risk money / stop distance).
  4. Validates the live contract (qty, sl, tp) with check_signal(live=True).
  5. Builds a TradeIntent and passes it through check_intent.

This module never sends an order. Approved intents go to the POV slicer in a later step,
and only in sandbox/analyzer mode (the execution layer's client module is the only place that sends orders).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Optional

from quant.core.config import RiskLimits
from quant.core.types import GateDecision, TradeIntent
from quant.monitor.safe_mode import SafeMode
from quant.risk.controls import HaltHandler
from quant.risk.gate import RiskState, check_intent
from quant.risk.kill_switch import KillSwitch
from quant.risk.pre_gate import PreGate
from quant.strategies.signals.base import check_signal


@dataclass(frozen=True)
class GatedSignal:
    ok: bool
    reason: str
    intent: Optional[TradeIntent] = None
    decision: Optional[GateDecision] = None


def levels_from_signal(sig: dict) -> tuple[float, float, float]:
    """Return (entry, sl, tp). BUY: sl below entry, tp above. SELL: the reverse."""
    side = str(sig["side"]).upper()
    entry = float(sig["entry"])
    sl_dist = float(sig["sl_dist"])
    tp_dist = float(sig["tp_dist"])
    if sl_dist <= 0 or tp_dist <= 0:
        raise ValueError("sl_dist and tp_dist must be > 0")
    if side == "BUY":
        return entry, entry - sl_dist, entry + tp_dist
    return entry, entry + sl_dist, entry - tp_dist


def size_from_risk(equity: float, risk_fraction: float, entry: float, sl: float) -> int:
    """Shares so that a stop-out loses about equity * risk_fraction. Returns 0 when inputs are unusable."""
    per_share = abs(entry - sl)
    if equity <= 0 or per_share <= 0 or not 0.0 < risk_fraction <= 0.05:
        return 0
    return int(math.floor(equity * risk_fraction / per_share))


def to_live_signal(sig: dict, equity: float, risk_fraction: float) -> dict | None:
    """Add qty, sl and tp to a strategy signal. Returns None when the signal cannot be sized."""
    entry, sl, tp = levels_from_signal(sig)
    qty = size_from_risk(equity, risk_fraction, entry, sl)
    if qty <= 0:
        return None
    return {**sig, "entry": entry, "sl": sl, "tp": tp, "qty": qty}


def _clamp_to_caps(live: dict, limits: RiskLimits, state: RiskState) -> tuple[Optional[dict], str]:
    """Lower qty to the per-order caps and the room left under max_open_exposure.

    Returns (live, note), or (None, note) when the clamped qty is below 1. A cap that is None is
    not applied here; the gate still refuses missing limits (fail closed)."""
    qty = int(live["qty"])
    entry = float(live["entry"])
    caps = {"signal": qty}
    if limits.max_order_quantity is not None:
        caps["max_order_quantity"] = int(limits.max_order_quantity)
    if limits.max_order_notional is not None and entry > 0:
        caps["max_order_notional"] = math.floor(float(limits.max_order_notional) / entry)
    if limits.max_open_exposure is not None and entry > 0:
        room = max(float(limits.max_open_exposure) - float(state.open_notional), 0.0)
        caps["open_exposure_room"] = math.floor(room / entry)
    name = min(caps, key=lambda k: caps[k])
    new_qty = int(caps[name])
    if new_qty < 1:
        return None, f"{name} allows 0 shares"
    if new_qty < qty:
        return {**live, "qty": new_qty}, f"qty {qty} clamped to {new_qty} by {name}"
    return live, ""


def gate_signal(
    sig: dict,
    *,
    symbol: str,
    strategy_id: str,
    equity: float,
    risk_fraction: float,
    state: RiskState,
    limits: RiskLimits,
    kill: KillSwitch,
    safe: SafeMode,
    halt: Optional[HaltHandler] = None,
    available_funds: Optional[float] = None,
    trade_day: Optional[date] = None,
    adv_shares: Optional[float] = None,
    prev_close: Optional[float] = None,
    circuit_pct: Optional[float] = None,
    lot_size: int = 1,
    asm_gsm: bool = False,
    algo_id: Optional[str] = None,
    pre_gate: Optional[PreGate] = None,
) -> GatedSignal:
    """Turn one signal into an approved or rejected intent. The caller records approved ids in state.seen_ids.

    Market fields (adv_shares, prev_close, circuit_pct) come from the live feed. Left as None, the gate
    fails closed for entries (for example "ADV unknown: no new entries")."""
    try:
        live = to_live_signal(sig, equity, risk_fraction)
    except (KeyError, TypeError, ValueError) as exc:
        return GatedSignal(False, f"bad signal: {exc}")
    if live is None:
        return GatedSignal(False, "size is zero (equity, risk or stop distance)")

    # Clamp to hard caps before the gate, so a valid signal is sized down, not rejected.
    # Only ever reduces qty. The gate still checks every limit after this.
    live, clamp_note = _clamp_to_caps(live, limits, state)
    if live is None:
        return GatedSignal(False, f"size is zero after caps ({clamp_note})")

    ok, reason = check_signal(live, live=True)
    if not ok:
        return GatedSignal(False, reason)

    # Build the intent first so the pre-gate checks the REAL size and ref price.
    side = str(live["side"]).upper()
    intent = TradeIntent(
        intent_id=f"{strategy_id}:{symbol}:{side}:{live.get('time', '')}",
        strategy_id=strategy_id,
        symbol=symbol,
        side=side,
        qty=int(live["qty"]),
        ref_price=float(live["entry"]),
        kind="entry",
        lot_size=lot_size,
        prev_close=prev_close,
        circuit_pct=circuit_pct,
        adv_shares=adv_shares,
        asm_gsm=asm_gsm,
        trade_day=trade_day,
        algo_id=algo_id,
    )
    # Pre-gate (capital protection): calendar, event blackout, overnight gap, handoff
    # contract, stress/VaR. Runs BEFORE the risk gate. It can only refuse, never approve.
    if pre_gate is not None:
        pre = pre_gate.check(intent)
        if not pre.ok:
            return GatedSignal(False, "pre-gate: " + "; ".join(pre.reasons))

    decision = check_intent(
        intent, state, limits, kill, safe, available_funds=available_funds, halt=halt
    )
    return GatedSignal(decision.approved, "approved" if decision.approved else "; ".join(decision.reasons),
                       intent=intent, decision=decision)
