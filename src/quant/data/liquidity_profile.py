"""Liquidity tiers: how much of a name can be traded without moving it.

Flow stage: Liquidity screener
Status: PHASE 4 IMPLEMENTED (wrap; measurement only, cannot approve an order)
Existing code to reuse or wrap (do not copy):
    src/quant/blue_engine/core/liquidity.py  (LiquidityProfile,
        build_liquidity_profile, resolve_liquidity_tier, LIQUIDITY_TIERS,
        calculate_max_child_qty, scale_block_size_pct, get_tier_default_max_block_pct)

WHY THIS IS SEPARATE FROM ``risk/liquidity.py``
-----------------------------------------------
``risk/liquidity.py`` answers ONE question, at the gate: "is this order size a
legal share of ADV?" It is a hard cap and it fails closed. Correct, and it stays.

It cannot answer the question the SCREENER asks: "which tier is this name in, and
how much can be worked through it at all?" The engine models five tiers
(MEGA/LARGE/MID/SMALL/MICRO) with per-tier block-size ceilings, which is the
difference between "this order is legal" and "this name is tradeable at size".

WHAT THIS WRAPPER ADDS
----------------------
1. **A screening verdict** (`assess`) the screener can filter on, separate from
   the gate's hard cap.
2. **Fail-closed on unknown liquidity.** A zero or unreadable daily value is
   MICRO, not "assume liquid". The engine already does this; the wrapper keeps it
   and says so in the verdict.
3. **A stable dict shape** for the dashboard, so a tier shows without the page
   knowing the engine's class.

WHAT IT DOES NOT DO
-------------------
- It does not place, size or approve an order. ``risk/gate.py`` and
  ``risk/liquidity.py`` remain the only hard caps.
- No market data access: volume and price are passed in.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from quant.core.blue_bridge import BlueEngineUnavailable, load

logger = logging.getLogger(__name__)

__all__ = [
    "LiquidityProfileUnavailable",
    "TIER_ORDER",
    "profile",
    "tier_of",
    "max_child_qty",
    "assess",
]

#: Weakest last. A screener can compare rank by index.
TIER_ORDER = ("MICRO", "SMALL", "MID", "LARGE", "MEGA")


class LiquidityProfileUnavailable(RuntimeError):
    """The engine's liquidity module cannot be loaded."""


def _module():
    try:
        return load("core.liquidity")
    except BlueEngineUnavailable as exc:  # pragma: no cover - environment dependent
        raise LiquidityProfileUnavailable(f"engine liquidity unavailable: {exc}") from exc


def tier_of(daily_volume: float, price: float) -> str:
    """Tier name for a name trading ``daily_volume`` shares at ``price``.

    An unusable value resolves to MICRO, matching the engine. That is the safe
    direction: treating an unknown name as maximally liquid is how a book ends up
    unable to exit.
    """
    try:
        value = float(daily_volume) * float(price)
    except (TypeError, ValueError):
        return "MICRO"
    if value <= 0:
        return "MICRO"
    try:
        return str(_module().resolve_liquidity_tier(value))
    except LiquidityProfileUnavailable:
        return "MICRO"


def profile(symbol: str, exchange: str, daily_volume: float, reference_price: float,
            *, target_investment: float = 0.0) -> Optional[Dict[str, Any]]:
    """Full liquidity profile as a dict, or None when the engine is unavailable."""
    try:
        mod = _module()
        prof = mod.build_liquidity_profile(
            symbol=symbol, exchange=exchange, daily_volume=int(daily_volume or 0),
            reference_price=float(reference_price or 0.0),
            target_investment=float(target_investment or 0.0))
    except LiquidityProfileUnavailable as exc:
        logger.warning("liquidity profile unavailable: %s", exc)
        return None
    except Exception as exc:  # noqa: BLE001 - a panel must render, not crash
        logger.warning("build_liquidity_profile failed: %r", exc)
        return None

    as_dict = getattr(prof, "to_dict", None)
    if callable(as_dict):
        return dict(as_dict())
    # Fall back to the dataclass fields, without guessing attribute names.
    return {k: getattr(prof, k) for k in dir(prof)
            if not k.startswith("_") and not callable(getattr(prof, k))}


def max_child_qty(symbol: str, exchange: str, daily_volume: float,
                  reference_price: float, target_qty: float) -> Optional[int]:
    """Largest single child the engine would allow for this name.

    This is the execution-side ceiling (per child), NOT the gate's total-order
    cap. Both apply; the smaller wins.
    """
    try:
        prof = _module().build_liquidity_profile(
            symbol=symbol, exchange=exchange, daily_volume=int(daily_volume or 0),
            reference_price=float(reference_price or 0.0),
            target_investment=float(target_qty or 0) * float(reference_price or 0.0))
        return int(_module().calculate_max_child_qty(prof, float(target_qty or 0)))
    except LiquidityProfileUnavailable:
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("calculate_max_child_qty failed: %r", exc)
        return None


def assess(symbol: str, daily_volume: float, price: float, *,
           min_tier: str = "SMALL") -> Dict[str, Any]:
    """Screener verdict for one name.

        {"symbol", "tier_rank", "daily_value_inr", "passes",
         "detail"}

    ``passes`` is False for any tier below ``min_tier``, and False when the
    liquidity value is unusable. An unknown name does not pass: a screener that
    admits names it cannot measure is a screener that admits illiquid ones.
    """
    try:
        daily_value = float(daily_volume) * float(price)
    except (TypeError, ValueError):
        daily_value = 0.0
    tier = tier_of(daily_volume, price)

    floor = str(min_tier or "SMALL").upper()
    if floor not in TIER_ORDER:
        raise ValueError(f"min_tier must be one of {TIER_ORDER}, got {min_tier!r}")

    rank = TIER_ORDER.index(tier) if tier in TIER_ORDER else 0
    floor_rank = TIER_ORDER.index(floor)
    passes = daily_value > 0 and rank >= floor_rank

    if daily_value <= 0:
        detail = "no usable ADV/price: treated as MICRO"
    elif passes:
        detail = f"tier {tier} meets the {floor} floor"
    else:
        detail = f"tier {tier} below the {floor} floor"

    return {"symbol": str(symbol).upper(), "tier": tier, "tier_rank": rank,
            "daily_value_inr": round(daily_value, 2), "passes": passes,
            "detail": detail}


def tier_table() -> List[Dict[str, Any]]:
    """The engine's tier thresholds, for a dashboard legend."""
    try:
        return [{"tier": name, "threshold_inr": float(thr)}
                for name, thr, *_ in _module().LIQUIDITY_TIERS]
    except Exception:  # noqa: BLE001
        return []
