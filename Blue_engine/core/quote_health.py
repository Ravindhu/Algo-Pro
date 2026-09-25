"""
quote_health.py - Quote-failure counter and PAUSED-state transition
(Feature 12 - Quote Failure Threshold & Persistence).

Responsibility
--------------
When the broker's quote / depth feed goes stale (WebSocket drop,
shallow-asset data drought, illiquid scrip), the slicer must NOT
retry forever - that wastes CPU and risks acting on a phantom
price. Instead:

  * Count every consecutive failed, empty, or stale data frame.
  * When the count reaches a configured threshold (default ``3``)
    we transition to a ``PAUSED`` state. The POV loop checks the
    counter at the top of every poll and short-circuits.
  * On the next successful frame the counter resets to zero, so
    a temporary blip does not lock the loop forever.

A quote is considered "valid" only when ALL of the following are
true:

  * the SDK response has ``status == "success"``,
  * the response carries a non-empty ``data`` object,
  * either a positive LTP OR a positive traded volume is present
    (one of the two is always there for an actively-traded scrip),
  * the value is not a "stale" sentinel (zero / negative / NaN).

Design rules
------------
* Pure state machine: the class holds three pieces of state and
  exposes three methods. No SDK calls, no DB writes, no network.
* Thread-safe: the POV loop may run on a background greenlet; a
  ``threading.Lock`` guards the counter so a concurrent update
  from the order-update stream cannot lose increments.
* Configurable threshold via the JSON config (``"quote_failure_
  threshold"``) and an environment variable fallback
  (``POV_QUOTE_FAILURE_THRESHOLD``).
"""
from __future__ import annotations

import math
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Optional


def _env_int(name: str, default: int) -> int:
    """Read a positive integer from the environment, with a default."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


DEFAULT_QUOTE_FAILURE_THRESHOLD: int = _env_int("POV_QUOTE_FAILURE_THRESHOLD", 3)


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------
def quote_frame_is_valid(quote_resp: Any) -> bool:
    """Return ``True`` if a quote/depth response is usable."""
    if not isinstance(quote_resp, dict):
        return False
    if quote_resp.get("status") != "success":
        return False
    data = quote_resp.get("data")
    if not isinstance(data, dict) or not data:
        return False

    for key in ("ltp", "last_price", "lastPrice", "lp", "close"):
        raw = data.get(key)
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            return True

    for key in ("volume", "volume_traded", "traded_volume", "volumeTraded", "v"):
        raw = data.get(key)
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            return True
    return False


# ---------------------------------------------------------------------------
# The state machine
# ---------------------------------------------------------------------------
@dataclass
class QuoteHealthMonitor:
    """Track consecutive quote failures and emit PAUSED when needed."""

    threshold: int = DEFAULT_QUOTE_FAILURE_THRESHOLD
    failures: int = 0
    paused: bool = False
    last_reason: Optional[str] = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        if self.threshold < 1:
            self.threshold = DEFAULT_QUOTE_FAILURE_THRESHOLD

    def record_success(self) -> None:
        """Reset the failure counter and clear any active PAUSED state."""
        with self._lock:
            self.failures = 0
            if self.paused:
                self.paused = False
                self.last_reason = None

    def record_failure(self, reason: str) -> bool:
        """Increment the failure counter and possibly transition to PAUSED."""
        with self._lock:
            self.failures += 1
            self.last_reason = reason
            if not self.paused and self.failures >= self.threshold:
                self.paused = True
                return True
            return False

    def should_pause(self) -> bool:
        """Return ``True`` if the slicer must skip the current poll."""
        with self._lock:
            return self.paused

    def snapshot(self) -> dict[str, Any]:
        """Return a small dict describing the current state."""
        with self._lock:
            return {
                "paused": self.paused,
                "failures": self.failures,
                "threshold": self.threshold,
                "last_reason": self.last_reason,
            }


__all__ = [
    "DEFAULT_QUOTE_FAILURE_THRESHOLD",
    "QuoteHealthMonitor",
    "quote_frame_is_valid",
]
