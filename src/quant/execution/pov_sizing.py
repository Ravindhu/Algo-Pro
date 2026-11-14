"""POV child sizing helpers (decouple stage 6.5, Phase B). Pure functions, no I/O.

Flow stage: POV child sizing
Status: PHASE 3 IMPLEMENTED (2026-11-09). Logic from src/quant/blue_engine/pov/slicers/pov.py (per-poll slice
maths, slippage penalty, remaining/max-slice/lot clamps). Parity: tests/parity/test_pov_phase_b_parity.py.
"""
from __future__ import annotations

from quant.execution.lot import lot_quantity


def poll_slice_qty(delta_volume: float, rate_pct: float) -> int:
    """Child quantity for one poll: ``int(delta_volume * rate_pct / 100)`` (truncates toward zero)."""
    return int((float(delta_volume) * float(rate_pct)) / 100.0)


def slippage_penalised_rate(rate_pct: float, *, over_threshold: bool, penalty: float,
                            floor_pct: float) -> float:
    """If realised slippage is over the threshold, scale the rate by ``penalty`` but never below the floor."""
    if not over_threshold:
        return float(rate_pct)
    return max(float(floor_pct), float(rate_pct) * max(0.0, float(penalty)))


def clamp_child_qty(slice_qty: int, *, remaining: int, max_slice: int | None,
                    lot_size: int | None) -> int:
    """Apply the remaining-quantity cap, the per-slice cap, then exchange-lot rounding (in that order).

    A non-positive remaining quantity gives 0 (nothing left to send).
    """
    qty = int(slice_qty)
    if qty <= 0:
        return 0
    if remaining <= 0:
        return 0
    qty = min(qty, int(remaining))
    if max_slice is not None and qty > int(max_slice):
        qty = int(max_slice)
    if lot_size:
        qty = lot_quantity(qty, int(lot_size))
    return max(0, qty)
