"""Monetary limit checks for ENTRY intents. Pure Python, no Blue_engine import.

Flow stage: Risk gate (monetary breach checks)
Status: PHASE 1 IMPLEMENTED
Existing code to reuse or wrap (do not copy): none. This is a PORT of src/quant/blue_engine/core/risk_gate.py
(RiskGate.check), done in DECOUPLING_PLAN stage 4 so src/quant no longer needs Blue for the money checks.
Parity with Blue: tests/parity/test_money_checks_parity.py (zero differences on the grid).

WHAT IS CHECKED (entries only)
------------------------------
1. Action is in the allowed list (BUY, SELL).
2. Quantity is a positive integer.
3. Quantity <= max_order_quantity.
4. Quantity x price <= max_order_notional (needs a finite positive price).
5. Today's P&L is not at or below -max_daily_loss (needs a verified P&L value).
6. Current open exposure + this order <= max_open_exposure (needs verified exposure and price).

A limit that is not set (None) is not checked here. The "limit not set" policy
(fail closed on a missing entry limit) lives in pretrade.py and loss_breaker.py.

FAIL-CLOSED RULES (copied from Blue)
------------------------------------
- A configured limit that cannot be checked is a breach. Examples: no price, NaN P&L.
- Any unexpected error returns a breach. The check never raises.
- Non-finite numbers (NaN, inf) are treated as missing.

NOT PORTED (Blue features the quant gate does not use)
------------------------------------------------------
- Master 'blocked' switch and the live acknowledgement (quant has its own kill switch and live guard).
- Exit reduction mode (reduces_exposure). Exits never reach this check.
- Config-level fallbacks for open_exposure and price. The quant gate passes these values in directly.
"""
from __future__ import annotations

import logging
import math
from typing import Any, List, Optional

from quant.core.config import RiskLimits
from quant.core.types import TradeIntent

_log = logging.getLogger("quant.risk.money_checks")

ALLOWED_ACTIONS = ("BUY", "SELL")


def _finite(value: Any) -> Optional[float]:
    """Return a finite float, or None for None, bad text, NaN and inf (same rule as Blue)."""
    if value is None or value == "":
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _int_or_none(value: Any) -> Optional[int]:
    f = _finite(value)
    return None if f is None else int(f)


def money_breaches(
    action: str,
    quantity: Any,
    price: Any,
    *,
    max_quantity: Any,
    max_notional: Any,
    max_daily_loss: Any,
    max_exposure: Any,
    day_pnl: Any,
    open_exposure: Any,
) -> List[str]:
    """Breach messages for one entry order. Empty list = pass.

    The message text matches Blue's RiskGate exactly, so parity can compare strings.
    A limit passed as None is not checked.
    """
    breaches: List[str] = []
    try:
        side = str(action).upper()
        qty = _int_or_none(quantity) or 0
        ref_price = _finite(price)
        if ref_price is not None and ref_price <= 0:
            ref_price = None
        notional = None if ref_price is None else qty * ref_price

        cap_qty = _int_or_none(max_quantity)
        cap_notional = _finite(max_notional)
        cap_loss = _finite(max_daily_loss)
        cap_exposure = _finite(max_exposure)

        # 1-2. Action and quantity.
        if side not in ALLOWED_ACTIONS:
            breaches.append(f"action {side!r} not in allowed_actions {list(ALLOWED_ACTIONS)}")
        if qty <= 0:
            breaches.append(f"quantity {qty} must be a positive integer")

        # 3. Size cap.
        if cap_qty is not None and qty > cap_qty:
            breaches.append(f"quantity {qty} > max_order_quantity {cap_qty}")

        # 4. Notional cap. Needs a price.
        if cap_notional is not None:
            if notional is None:
                breaches.append("cannot evaluate max_order_notional without a finite positive order price")
            elif notional > cap_notional:
                breaches.append(f"notional {notional:.2f} > max_order_notional {cap_notional:.2f}")

        # 5. Daily loss cap. A loss is a negative P&L.
        if cap_loss is not None:
            pnl = _finite(day_pnl)
            if pnl is None:
                breaches.append("cannot evaluate max_daily_loss without a verified daily P&L value")
            elif pnl <= -abs(cap_loss):
                breaches.append(f"day_pnl {pnl:.2f} <= -max_daily_loss {abs(cap_loss):.2f}")

        # 6. Exposure cap. Current exposure plus this order must fit.
        if cap_exposure is not None:
            current = _finite(open_exposure)
            if current is None:
                breaches.append("cannot evaluate max_open_exposure without verified current exposure")
            elif notional is None:
                breaches.append("cannot evaluate max_open_exposure without a finite positive order price")
            else:
                projected = abs(current) + abs(notional)
                if projected > cap_exposure:
                    breaches.append(f"projected exposure {projected:.2f} > max_open_exposure {cap_exposure:.2f}")
    except Exception as exc:  # noqa: BLE001 - the gate must never raise
        _log.warning("money_checks: evaluation error: %r", exc)
        return [f"risk gate could not evaluate intent ({exc!r}); rejecting to be safe"]
    return breaches


def entry_breaches(
    intent: TradeIntent,
    limits: RiskLimits,
    *,
    open_notional: float,
    daily_pnl: float,
) -> List[str]:
    """Breach reasons for an ENTRY intent. Exits and protection orders always return []."""
    if intent.kind != "entry":
        return []
    if all(v is None for v in (
        limits.max_order_quantity, limits.max_order_notional,
        limits.max_daily_loss, limits.max_open_exposure,
    )):
        return []  # no monetary limit configured: nothing to check here (pretrade rejects it)
    return money_breaches(
        intent.side,
        intent.qty,
        intent.ref_price,
        max_quantity=limits.max_order_quantity,
        max_notional=limits.max_order_notional,
        max_daily_loss=limits.max_daily_loss,
        max_exposure=limits.max_open_exposure,
        day_pnl=daily_pnl,
        open_exposure=open_notional,
    )
