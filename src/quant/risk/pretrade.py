"""Pre-trade sanity checks: quantity, lot size, price, per-order caps.

Flow stage: Pre-trade checks
Status: PHASE 1 IMPLEMENTED
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/core/risk_gate.py (values in risk/money_checks.py)
"""
from __future__ import annotations

from typing import Any, List, Mapping, Optional

from quant.core.config import RiskLimits
from quant.core.types import TradeIntent


def pretrade_checks(intent: TradeIntent, limits: RiskLimits, require_algo_id: bool = False,
                    *, owner_limits: Optional[Mapping[str, Any]] = None) -> List[str]:
    """Entry policy checks, then the OWNER's hard caps from config/risk_limits.yaml.

    Why the owner caps are checked HERE (audit item 5, 2026-11-17): the owner
    file is the source of truth, but the blue-runner merge was the only thing
    applying it. The v5 gate is the single entry point for every order intent,
    so a caller that never called the merge could trade under looser caps. The
    merge stays (it tightens the blue config); this check is the backstop on
    this side: an owner cap stricter than the config's own cap refuses.
    """
    reasons: List[str] = []
    if require_algo_id and not intent.algo_id:
        reasons.append("algo ID missing on order (require_algo_id is on)")
    if intent.qty <= 0:
        reasons.append("qty must be > 0")
    if intent.lot_size <= 0 or intent.qty % intent.lot_size:
        reasons.append(f"qty {intent.qty} not a multiple of lot {intent.lot_size}")
    if intent.ref_price <= 0:
        reasons.append("ref_price must be > 0")
    if intent.kind != "entry":
        return reasons  # exits and protection orders reduce risk; per-order caps do not apply
    # Policy only: an entry needs its per-order caps configured. Values are checked by
    # risk/money_checks.py, not here.
    if limits.max_order_quantity is None:
        reasons.append("limit not set: max_order_quantity")
    if limits.max_order_notional is None:
        reasons.append("limit not set: max_order_notional")

    # Owner hard caps (audit item 5). A null owner cap is NOT SET, not disabled:
    # nothing is added for it and no threshold is invented for it (decision 0037).
    # Where BOTH sides set a cap, the owner's stricter value refuses; where only
    # the owner sets one, the config has no cap to compare, so the owner's own
    # value is enforced here directly rather than trusted to a merge the caller
    # may not have run.
    if owner_limits is not None:
        notional = intent.qty * intent.ref_price
        for name, value, actual in (
            ("max_order_quantity", owner_limits.get("max_order_quantity"), intent.qty),
            ("max_order_notional", owner_limits.get("max_order_notional"), notional),
        ):
            cap = _as_number(value)
            if cap is not None and actual > cap:
                reasons.append(
                    f"owner limit: {name} {actual:.2f} exceeds the owner cap {cap:.2f}")
    return reasons


def _as_number(value: Any) -> Optional[float]:
    """Finite float, else None. Mirrors owner_limits._as_number."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None
