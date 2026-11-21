"""Bounded peg price for entry LIMIT orders (2026-11-16, high-volume level 2026-11-21).

Flow stage: Order pricing
Status: PHASE 3 IMPLEMENTED (pure pricing; no order route)

OpenAlgo has no PEG order type, so the bot emulates it with a LIMIT order (for BUY).
Pricing rules:
- BUY candidates: each of the top ``depth_levels`` bid levels is a candidate, ranked by
  resting quantity (largest first, ties to the higher price). The order price is that
  level plus ``offset_ticks`` ticks, and never at or above the best ask.
- SELL: the first ask level, unchanged.
- No level, no price, or a non-finite price gives no candidate. The caller then uses the fixed path.
- A candidate above the cap that the caller passes is dropped.
"""
from __future__ import annotations

import math
from typing import Any, List, Optional, Tuple


def _level_fields(level: Any) -> Optional[Tuple[float, float]]:
    """(price, quantity) of one depth level, or None when it does not parse."""
    if not isinstance(level, dict) or isinstance(level.get("price"), bool):
        return None
    try:
        px = float(level.get("price"))
        qty = float(level.get("quantity", level.get("qty", 0)) or 0)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(px) or px <= 0 or not math.isfinite(qty) or qty < 0:
        return None
    return px, qty


def peg_candidates(side: str, depth_reply: Any, cap: Optional[float] = None, tick: float = 0.0,
                   depth_levels: int = 5, offset_ticks: int = 1) -> List[Tuple[float, int, float]]:
    """[(order price, bid qty at level, level price)] best first, or [] when no usable depth.

    For BUY the list is ranked by quantity at the level (largest first, ties to the higher
    price). Each price is the level plus ``offset_ticks`` ticks, kept below the best ask.
    """
    if not isinstance(depth_reply, dict) or str(depth_reply.get("status", "")).lower() != "success":
        return []
    data = depth_reply.get("data")
    if not isinstance(data, dict):
        return []
    side_u = str(side).upper()
    levels = data.get("bids" if side_u == "BUY" else "asks")
    if not isinstance(levels, list) or not levels:
        return []
    out: List[Tuple[float, int, float]] = []
    if side_u == "BUY":
        best_ask = None
        asks = data.get("asks")
        if isinstance(asks, list) and asks:
            ask_fields = _level_fields(asks[0])
            best_ask = ask_fields[0] if ask_fields else None
        for raw in levels[: max(1, int(depth_levels))]:
            parsed = _level_fields(raw)
            if parsed is None:
                continue
            lvl_px, lvl_qty = parsed
            px = lvl_px + max(0, int(offset_ticks)) * tick if tick > 0 else lvl_px
            if tick > 0 and best_ask is not None and px >= best_ask:
                px = max(lvl_px, best_ask - tick)  # never cross the spread
            px = round(px, 2)
            if cap is not None and px > cap:
                continue
            out.append((px, int(lvl_qty), round(lvl_px, 2)))
        out.sort(key=lambda c: (c[1], c[2]), reverse=True)
        return out
    parsed = _level_fields(levels[0])
    if parsed is None:
        return []
    px, qty = parsed
    px = round(px, 2)
    if cap is not None and px > cap:
        return []
    return [(px, int(qty), px)]


def peg_level(side: str, depth_reply: Any, cap: Optional[float] = None,
              tick: float = 0.0, depth_levels: int = 5, offset_ticks: int = 1
              ) -> Optional[Tuple[float, int]]:
    """(price, quantity) of the best candidate, or None."""
    cands = peg_candidates(side, depth_reply, cap, tick, depth_levels, offset_ticks)
    if not cands:
        return None
    px, qty, _ = cands[0]
    return px, qty


def peg_price(side: str, depth_reply: Any, cap: Optional[float] = None) -> Optional[float]:
    level = peg_level(side, depth_reply, cap)
    return level[0] if level else None


__all__ = ["peg_candidates", "peg_level", "peg_price"]
