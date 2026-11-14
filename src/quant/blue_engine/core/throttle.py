"""
throttle.py - Order-to-Trade Ratio (OTR) and minimum-interval throttling
(Feature 10 - Rate-Limiting & Throttling Helper).

Responsibility
--------------
Expose a single helper, :func:`should_throttle_order`, that the POV
loop calls BEFORE every ``client.placeorder(...)`` payload. The
helper enforces three hard tracking parameters from the JSON
config (each with a safe default):

  * ``max_order_to_trade_ratio``     - hard cap on (orders / trades)
                                       to keep OTR below the exchange
                                       penalty threshold.
  * ``min_order_interval_seconds``   - minimum gap between any two
                                       child orders (anti-burst).
  * ``cancellation_cool_off_seconds`` - mandatory pause after a
                                       cancel, so we don't get into
                                       modify-cancel-modify cycles.

The decision logic mirrors the proven formula in
``services/volume_participation_service.py:should_throttle_order`` -
take the LONGER of (cool-off elapsed, interval elapsed) so the
slicer respects both at once. The OTR ratio is evaluated AFTER
the interval check; failing the OTR triggers a pause of one
poll cycle to let some trades come in.

Design rules
------------
* Pure function - no SDK calls, no DB writes, no network. Caller
  passes a snapshot of the execution state.
* Always returns ``(throttled, delay, reason)`` so the loop can
  log a precise reason and sleep for exactly the right amount.
* Defaults match the production values used by the broker-facing
  service (``POV_ORDER_TO_TRADE_RATIO=10``, ``POV_MIN_ORDER_INTERVAL
  _SECONDS=1``, ``POV_CANCEL_COOL_OFF_SECONDS=2``).
"""
from __future__ import annotations

import math
import os
from datetime import datetime, timezone
from typing import Any, Tuple


def _env_float(name: str, default: float) -> float:
    """Read a non-negative float from the environment, with a default.

    Returns ``default`` on any error (missing key, non-numeric, or
    negative). Used at import time so the defaults are stable.
    """
    try:
        raw = os.getenv(name)
        value = float(raw) if raw is not None else default
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) and value >= 0 else default


DEFAULT_ORDER_TO_TRADE_RATIO: float = max(
    1.0, _env_float("POV_ORDER_TO_TRADE_RATIO", 10.0)
)
DEFAULT_MIN_ORDER_INTERVAL_SECONDS: float = _env_float(
    "POV_MIN_ORDER_INTERVAL_SECONDS", 1.0
)
DEFAULT_CANCEL_COOL_OFF_SECONDS: float = _env_float(
    "POV_CANCEL_COOL_OFF_SECONDS", 2.0
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def should_throttle_order(
    execution: Any,
    request_data: dict[str, Any],
    now: datetime | None = None,
) -> Tuple[bool, float, str | None]:
    """Decide whether the next child order must be delayed.

    Returns ``(throttled, delay, reason)``.
    """
    # Naive UTC, identical to the deprecated utcnow(). ``timezone.utc`` not
    # ``datetime.UTC``: the latter exists only on Python 3.11+.
    current = now or datetime.now(timezone.utc).replace(tzinfo=None)

    cool_off = float(
        request_data.get(
            "cancellation_cool_off_seconds",
            request_data.get("cancel_cool_off_seconds", DEFAULT_CANCEL_COOL_OFF_SECONDS),
        )
    )
    min_interval = float(
        request_data.get(
            "min_order_interval_seconds", DEFAULT_MIN_ORDER_INTERVAL_SECONDS
        )
    )
    ratio = float(
        request_data.get(
            "max_order_to_trade_ratio",
            request_data.get("order_to_trade_ratio", DEFAULT_ORDER_TO_TRADE_RATIO),
        )
    )

    # 1. The slicer must respect BOTH the cool-off AND the minimum
    #    interval. Take the LONGER of the two so we never shortchange
    #    either constraint.
    waits = []  # list of (delay_seconds, reason)
    last_cancel = getattr(execution, "last_cancel_at", None)
    if last_cancel is not None:
        elapsed = (current - last_cancel).total_seconds()
        waits.append((cool_off - elapsed, "cancellation cooling-off"))
    last_order = getattr(execution, "last_order_at", None)
    if last_order is not None:
        elapsed = (current - last_order).total_seconds()
        waits.append((min_interval - elapsed, "order interval"))
    if waits:
        delay, reason = max(waits)
        if delay is not None and delay > 0:
            return True, float(delay), reason

    # 2. Order-to-Trade Ratio guard.
    orders = int(getattr(execution, "order_count", 0) or 0)
    filled = int(getattr(execution, "filled_quantity", 0) or 0) + int(
        getattr(execution, "exit_filled_quantity", 0) or 0
    )
    ratio_limit = max(1, math.ceil(max(1, filled) * ratio))
    if orders >= ratio_limit:
        interval = float(request_data.get("interval", 1))
        return True, max(0.1, interval), "order-to-trade ratio"

    return False, 0.0, None


__all__ = [
    "DEFAULT_ORDER_TO_TRADE_RATIO",
    "DEFAULT_MIN_ORDER_INTERVAL_SECONDS",
    "DEFAULT_CANCEL_COOL_OFF_SECONDS",
    "should_throttle_order",
]
