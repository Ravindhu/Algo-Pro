"""Transaction cost analysis: slippage against arrival price. Feeds the cost model.

Flow stage: TCA to cost model
Status: PHASE 3 IMPLEMENTED; defects fixed and feedback added 2026-11-14
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/backtest/costs.py
    (CostModel.calibrate / slippage_bps - the modelled parameter this feeds)

FOUR DEFECTS FIXED
------------------
1. ``arrival = 0`` raised ZeroDivisionError. A missing arrival price is a data
   problem, not an exception at the analysis boundary.
2. A NaN fill produced a NaN slippage, which then POISONED the mean: one bad row
   made the whole period's slippage NaN, so the number silently stopped meaning
   anything.
3. An unknown side ("SHORT", a typo, an empty string) was treated as SELL and
   returned a NEGATIVE slippage - the sign flip makes a cost look like a gain.
   Only BUY and SELL are accepted now.
4. **The header claimed it "feeds the cost model" and nothing did.** The model's
   slippage parameter was never updated from live fills, so research always ran
   on the assumed number. ``calibrated_slippage_bps`` closes that loop.

SIGN CONVENTION (unchanged)
---------------------------
Positive = worse than arrival, i.e. a cost to us. A BUY filled above arrival and
a SELL filled below arrival are both positive.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, Optional, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "slippage_bps",
    "mean_slippage_bps",
    "calibrated_slippage_bps",
    "tca_report",
]

_VALID_SIDES = ("BUY", "SELL")


def slippage_bps(side: str, fill: float, arrival: float) -> float:
    """Positive = worse than arrival (cost to us). NaN when unmeasurable.

    Returns NaN rather than raising for a bad PRICE, so one unusable row cannot
    abort a whole period's analysis; ``mean_slippage_bps`` skips NaN entries.
    Raises ValueError only for a bad SIDE, which is a programming error.
    """
    code = str(side or "").strip().upper()
    if code not in _VALID_SIDES:
        raise ValueError(f"side must be BUY or SELL, got {side!r}")

    try:
        f = float(fill)
        a = float(arrival)
    except (TypeError, ValueError):
        return float("nan")
    if not (math.isfinite(f) and math.isfinite(a)) or a <= 0 or f <= 0:
        return float("nan")

    raw = (f - a) / a * 1e4
    return float(raw if code == "BUY" else -raw)


def mean_slippage_bps(rows: Sequence[dict]) -> float:
    """Average slippage over usable rows. NaN rows are SKIPPED, not propagated.

    Returns 0.0 when nothing is measurable. A caller that needs to tell "zero
    slippage" from "no data" should use :func:`tca_report`, which reports the
    sample count.
    """
    values = []
    for row in rows or []:
        try:
            value = slippage_bps(row.get("side"), row.get("fill"), row.get("arrival"))
        except ValueError as exc:
            logger.warning("TCA row skipped: %s", exc)
            continue
        if math.isfinite(value):
            values.append(value)
    if not values:
        return 0.0
    return float(sum(values) / len(values))


def tca_report(rows: Sequence[dict], *, min_samples: int = 20) -> Dict[str, Any]:
    """Slippage summary with the sample size, for the dashboard and calibration.

        {"n_total", "n_usable", "mean_bps", "median_bps", "worst_bps",
         "ready": bool, "detail": str}

    ``ready`` is False below ``min_samples``: calibrating a cost model on five
    fills is fitting noise, and a small negative mean would then LOWER the assumed
    cost and flatter every future backtest.
    """
    usable = []
    for row in rows or []:
        try:
            value = slippage_bps(row.get("side"), row.get("fill"), row.get("arrival"))
        except ValueError:
            continue
        if math.isfinite(value):
            usable.append(value)

    total = len(rows or [])
    if not usable:
        return {"n_total": total, "n_usable": 0, "mean_bps": None,
                "median_bps": None, "worst_bps": None, "ready": False,
                "detail": "no measurable fills"}

    usable.sort()
    n = len(usable)
    median = usable[n // 2] if n % 2 else (usable[n // 2 - 1] + usable[n // 2]) / 2.0
    ready = n >= int(min_samples)
    return {
        "n_total": total,
        "n_usable": n,
        "mean_bps": round(sum(usable) / n, 4),
        "median_bps": round(median, 4),
        "worst_bps": round(usable[-1], 4),
        "ready": ready,
        "detail": (f"{n} measurable fill(s)" if ready
                   else f"{n} fill(s); need {int(min_samples)} before calibrating"),
    }


def calibrated_slippage_bps(rows: Sequence[dict], *, assumed_bps: float,
                            min_samples: int = 20,
                            blend: float = 0.5) -> Dict[str, Any]:
    """Turn live fills into a cost-model slippage assumption.

    This is the feedback the module header always claimed. It blends the measured
    mean with the existing assumption rather than replacing it, because a single
    week of fills is not a calibration:

        new = assumed * (1 - blend) + measured * blend

    The result is FLOORED at zero: slippage is a cost, and a measured negative
    mean (we happened to fill better than arrival) must not become a credit that
    makes every future backtest look cheaper.

    Returns ``{"applied": bool, "assumed_bps", "measured_bps", "new_bps",
    "detail"}``. ``applied`` is False when there is not enough evidence, and the
    caller must then KEEP the old value.
    """
    report = tca_report(rows, min_samples=min_samples)
    if not report["ready"]:
        return {"applied": False, "assumed_bps": assumed_bps, "measured_bps": None,
                "new_bps": assumed_bps, "detail": report["detail"]}

    measured = float(report["mean_bps"])
    blended = float(assumed_bps) * (1 - float(blend)) + measured * float(blend)
    new_bps = max(0.0, blended)
    return {
        "applied": True,
        "assumed_bps": float(assumed_bps),
        "measured_bps": round(measured, 4),
        "new_bps": round(new_bps, 4),
        "detail": (f"blended {float(assumed_bps):.2f} with measured {measured:.2f} "
                   f"over {report['n_usable']} fills"),
    }
