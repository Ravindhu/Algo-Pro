"""Bounded peg price for entry LIMIT orders (2026-11-16).

Flow stage: Order pricing
Status: PHASE 3 IMPLEMENTED (pure pricing; no order route)

OpenAlgo has no PEG order type, so the bot emulates it with a LIMIT order at the best bid
(for BUY). Pricing rules:
- Read the first level of the depth reply: bids for BUY, asks for SELL.
- No level, no price, or a non-finite price gives None. The caller then uses the fixed path.
- The peg is never above the cap that the caller passes (last close plus the entry slippage).
"""
from __future__ import annotations

import math
from typing import Any, Optional, Tuple


def peg_level(side: str, depth_reply: Any, cap: Optional[float] = None,
              tick: float = 0.0) -> Optional[Tuple[float, int]]:
    """(price, quantity) for a BUY peg, or None.

    price: the best bid, improved by one tick when that stays below the best ask (so our order sits
    ahead of the resting bid queue and never crosses the spread). quantity: shares resting at the best bid.
    """
    if not isinstance(depth_reply, dict) or str(depth_reply.get("status", "")).lower() != "success":
        return None
    data = depth_reply.get("data")
    if not isinstance(data, dict):
        return None
    levels = data.get("bids" if str(side).upper() == "BUY" else "asks")
    if not isinstance(levels, list) or not levels or not isinstance(levels[0], dict):
        return None
    ask_levels = data.get("asks")
    raw = levels[0].get("price")
    if isinstance(raw, bool):
        return None
    try:
        px = float(raw)
        qty = float(levels[0].get("quantity", levels[0].get("qty", 0)) or 0)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(px) or px <= 0 or not math.isfinite(qty) or qty < 0:
        return None
    if str(side).upper() == "BUY" and tick > 0:
        best_ask = None
        if isinstance(ask_levels, list) and ask_levels and isinstance(ask_levels[0], dict):
            try:
                best_ask = float(ask_levels[0].get("price"))
            except (TypeError, ValueError):
                best_ask = None
        improved = round(px + tick, 2)
        if best_ask is None or not math.isfinite(best_ask) or improved < best_ask:
            px = improved
    px = round(px, 2)
    if cap is not None and px > cap:
        return None
    return px, int(qty)


def peg_price(side: str, depth_reply: Any, cap: Optional[float] = None) -> Optional[float]:
    level = peg_level(side, depth_reply, cap)
    return level[0] if level else None


__all__ = ["peg_level", "peg_price"]
