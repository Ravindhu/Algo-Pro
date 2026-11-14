"""POV order throttle: minimum gaps and order-to-trade ratio (decouple 6.5, Step 1). Pure.

Flow stage: POV child sizing (throttle before each child order)
Status: PHASE 3 IMPLEMENTED (ported 2026-11-09 from src/quant/blue_engine/core/throttle.py; pure; no Blue import)
Parity: tests/parity/test_pov_step1_parity.py

Differences from Blue (deliberate):
  - Blue reads three POV_* environment variables at import time. Here the defaults are constants and
    every value comes from request_data. Hidden import-time env reads are global state, so they are gone.
  - Blue keeps last-order and last-cancel times in module-level dicts. Here they live in
    PovThrottleState, one per run, passed in explicitly.

Decision rule (same as Blue): take the LONGER of the cancel cool-off and the order interval; then
check the order-to-trade ratio. Returns (throttled, delay_seconds, reason).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional, Tuple

DEFAULT_ORDER_TO_TRADE_RATIO: float = 10.0
DEFAULT_MIN_ORDER_INTERVAL_SECONDS: float = 1.0
DEFAULT_CANCEL_COOL_OFF_SECONDS: float = 2.0


@dataclass
class PovThrottleState:
    """Per-run throttle state. Pass it in; never keep it at module level."""

    order_count: int = 0
    filled_quantity: int = 0
    exit_filled_quantity: int = 0
    last_order_at: Optional[datetime] = None
    last_cancel_at: Optional[datetime] = None

    def record_order(self, at: datetime) -> None:
        self.order_count += 1
        self.last_order_at = at

    def record_cancel(self, at: datetime) -> None:
        self.last_cancel_at = at

    def record_fill(self, qty: int, *, exit_leg: bool = False) -> None:
        if qty <= 0:
            return
        if exit_leg:
            self.exit_filled_quantity += int(qty)
        else:
            self.filled_quantity += int(qty)


def should_throttle_order(
    execution: Any,
    request_data: Mapping[str, Any],
    now: Optional[datetime] = None,
) -> Tuple[bool, float, Optional[str]]:
    """Decide whether the next child order must wait.

    ``execution`` needs order_count, filled_quantity, exit_filled_quantity, last_order_at and
    last_cancel_at (a PovThrottleState works). Times must be naive UTC, or all aware, to match.
    """
    current = now or datetime.now(timezone.utc).replace(tzinfo=None)
    cool_off = float(request_data.get(
        "cancellation_cool_off_seconds",
        request_data.get("cancel_cool_off_seconds", DEFAULT_CANCEL_COOL_OFF_SECONDS)))
    min_interval = float(request_data.get(
        "min_order_interval_seconds", DEFAULT_MIN_ORDER_INTERVAL_SECONDS))
    ratio = float(request_data.get(
        "max_order_to_trade_ratio",
        request_data.get("order_to_trade_ratio", DEFAULT_ORDER_TO_TRADE_RATIO)))

    waits = []
    last_cancel = getattr(execution, "last_cancel_at", None)
    if last_cancel is not None:
        waits.append((cool_off - (current - last_cancel).total_seconds(), "cancellation cooling-off"))
    last_order = getattr(execution, "last_order_at", None)
    if last_order is not None:
        waits.append((min_interval - (current - last_order).total_seconds(), "order interval"))
    if waits:
        delay, reason = max(waits)
        if delay is not None and delay > 0:
            return True, float(delay), reason

    orders = int(getattr(execution, "order_count", 0) or 0)
    filled = (int(getattr(execution, "filled_quantity", 0) or 0)
              + int(getattr(execution, "exit_filled_quantity", 0) or 0))
    ratio_limit = max(1, math.ceil(max(1, filled) * ratio))
    if orders >= ratio_limit:
        interval = float(request_data.get("interval", 1))
        return True, max(0.1, interval), "order-to-trade ratio"
    return False, 0.0, None


__all__ = [
    "DEFAULT_ORDER_TO_TRADE_RATIO", "DEFAULT_MIN_ORDER_INTERVAL_SECONDS",
    "DEFAULT_CANCEL_COOL_OFF_SECONDS", "PovThrottleState", "should_throttle_order",
]
