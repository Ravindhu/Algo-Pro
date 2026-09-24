"""
benchmark.py - Slippage / Arrival-Price helpers (Phase 2, Feature 13).

================================================================================
PURPOSE
================================================================================
Pure (no SDK, no DB) helpers that:

  1. Maintain a rolling ring buffer of mid-quote ticks recorded between
     ``placeorder`` send and fill arrival, so we can compute an
     **arrival-price VWAP** per child order.
  2. Compute signed slippage in basis points for an incoming fill.
  3. Maintain a rolling ring buffer of recent slippage samples so the
     slicer can average them and trigger a Phase-2 penalty when the
     average crosses a configured threshold.
  4. Resolve the dynamic ``expected_price`` for a fill by combining
     the session VWAP (from the ledger) and the arrival-price VWAP
     (from this module): ``max(session_vwap, arrival_vwap)``.

The arrival-price concept is the institutional standard: it measures
the market the order actually walked through, and strips out our own
impact. The session-VWAP fallback keeps cold-start sessions sane.

================================================================================
USAGE FROM THE POV SLICER
================================================================================
    from pov_engine.core.benchmark import (
        ArrivalPriceWindow,
        SlippageAccumulator,
        compute_slippage_bps,
        resolve_expected_price,
    )

    window = ArrivalPriceWindow(max_ticks=20)
    slip   = SlippageAccumulator(window=10)

    # 1) Per poll, after a successful quote:
    window.record(price=mid, volume_weight=delta_volume)

    # 2) Just before placeorder:
    window.clear()  # start fresh for the next child

    # 3) On fill (from the order-update WebSocket):
    arrival_vwap = window.vwap()
    expected     = resolve_expected_price(
                       session_vwap=ledger.get_vwap(execution_id),
                       arrival_vwap=arrival_vwap,
                   )
    bps = compute_slippage_bps(
        expected_price=expected,
        actual_price=fill_price,
        side="BUY",
    )
    slip.record(bps)
    window.clear()
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Optional


# -----------------------------------------------------------------------------
# Arrival-price window
# -----------------------------------------------------------------------------
@dataclass
class ArrivalPriceWindow:
    """Ring buffer of mid-quote ticks for one open child order.

    Each tick carries a price (mid) and a volume weight (delta_volume
    since the previous tick). The VWAP is the volume-weighted mean.

    The window is bounded: when full, the oldest tick is dropped. This
    matches the institutional definition of "arrival price" as the
    market that the order walked through during its lifetime.

    Attributes
    ----------
    max_ticks : int
        Maximum number of ticks retained. Default 20.
    """

    max_ticks: int = 20
    _buf: Deque["_QuoteTick"] = field(default_factory=deque)

    def __post_init__(self) -> None:
        if self.max_ticks < 1:
            self.max_ticks = 1
        # Re-create the deque with the correct maxlen (deque must be
        # constructed with maxlen, not mutated later).
        self._buf = deque(maxlen=self.max_ticks)

    def record(self, *, price: float, volume_weight: float = 1.0) -> None:
        """Append a tick. Silently ignores non-positive inputs."""
        if price is None or price <= 0:
            return
        if volume_weight is None or volume_weight <= 0:
            volume_weight = 1.0
        self._buf.append(_QuoteTick(price=float(price),
                                    volume_weight=float(volume_weight)))

    def vwap(self) -> Optional[float]:
        """Return the volume-weighted average, or ``None`` if empty."""
        if not self._buf:
            return None
        num = sum(t.price * t.volume_weight for t in self._buf)
        den = sum(t.volume_weight for t in self._buf)
        return (num / den) if den > 0 else None

    def tick_count(self) -> int:
        """Number of ticks currently buffered."""
        return len(self._buf)

    def clear(self) -> None:
        """Drop all buffered ticks. Call after a fill arrives."""
        self._buf.clear()


@dataclass(frozen=True)
class _QuoteTick:
    """One mid-quote observation. Private to this module."""
    price: float
    volume_weight: float


# -----------------------------------------------------------------------------
# Slippage accumulator (rolling window of bps samples)
# -----------------------------------------------------------------------------
@dataclass
class SlippageAccumulator:
    """Rolling average of recent slippage samples (in basis points).

    Attributes
    ----------
    window : int
        Maximum number of samples retained. Default 10.
    """

    window: int = 10
    _samples: Deque[float] = field(default_factory=deque)

    def __post_init__(self) -> None:
        if self.window < 1:
            self.window = 1
        self._samples = deque(maxlen=self.window)

    def record(self, bps: float) -> None:
        """Append one sample. NaN / inf are ignored."""
        if bps is None:
            return
        try:
            value = float(bps)
        except (TypeError, ValueError):
            return
        # NaN check: NaN != NaN
        if value != value or value in (float("inf"), float("-inf")):
            return
        self._samples.append(value)

    def current_bps(self) -> float:
        """Mean of the buffered samples. Returns 0.0 when empty.

        Returning 0.0 (not NaN) on cold-start is intentional: the
        slicer treats the first fill as "no penalty" by default.
        """
        if not self._samples:
            return 0.0
        return sum(self._samples) / len(self._samples)

    def is_over_threshold(self, threshold_bps: float) -> bool:
        """``True`` when the rolling mean exceeds ``threshold_bps``."""
        if threshold_bps is None:
            return False
        return self.current_bps() >= float(threshold_bps)

    def sample_count(self) -> int:
        return len(self._samples)

    def clear(self) -> None:
        self._samples.clear()


# -----------------------------------------------------------------------------
# Pure helpers
# -----------------------------------------------------------------------------
def compute_slippage_bps(
    *, expected_price: float, actual_price: float, side: str
) -> float:
    """Compute *adverse* slippage in basis points.

    The result is always >= 0 for adverse moves and <= 0 for favorable
    moves. ``side`` flips the sign convention so a SELL order's
    slippage is measured as "fill price below expected".

    Returns 0.0 when either price is missing or non-positive.
    """
    if expected_price is None or expected_price <= 0:
        return 0.0
    if actual_price is None or actual_price <= 0:
        return 0.0
    diff = float(actual_price) - float(expected_price)
    if str(side).upper() == "SELL":
        diff = -diff
    return (diff / float(expected_price)) * 10_000.0


def resolve_expected_price(
    *,
    session_vwap: Optional[float],
    arrival_vwap: Optional[float],
) -> Optional[float]:
    """Pick the larger of the two VWAPs as the benchmark.

    Rationale (from the design doc):

    * Mature session + quiet market  -> session_vwap wins (stable)
    * Mature session + volatile      -> arrival_vwap wins (reactive)
    * Cold start (few minutes in)    -> arrival_vwap wins (less noisy)
    * Single illiquid name           -> session_vwap wins (1 print dominates)

    Returns ``None`` when both are unavailable.
    """
    candidates = [v for v in (session_vwap, arrival_vwap)
                  if v is not None and v > 0]
    if not candidates:
        return None
    return max(candidates)


__all__ = [
    "ArrivalPriceWindow",
    "SlippageAccumulator",
    "compute_slippage_bps",
    "resolve_expected_price",
]

