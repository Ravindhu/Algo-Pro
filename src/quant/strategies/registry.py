"""Name -> class registry for the BUY strategies. Only the BUY side is registered (owner, 2026-11-08).

Flow stage: Strategy registry
Status: PHASE 2 IMPLEMENTED (paper only, ported 2026-11-08; not live). Sell-side blueprints are not ported and are not registered.
"""
from __future__ import annotations

from typing import Any

from quant.strategies.base import MultiTimeframeStrategy
from quant.strategies.engulfing_ocl import EngulfingOcl
from quant.strategies.ocl import OclKeylevel
from quant.strategies.ocl_sell_fail import Handoff, OclSellFailBottom, OclSellFailTop

BUY_STRATEGIES: dict[str, type[MultiTimeframeStrategy]] = {
    OclKeylevel.name: OclKeylevel,
    OclSellFailTop.name: OclSellFailTop,
    OclSellFailBottom.name: OclSellFailBottom,
    EngulfingOcl.name: EngulfingOcl,
}

# Sell-failure blueprints need the shared handoff store (per symbol).
_NEEDS_HANDOFF = {OclSellFailTop.name, OclSellFailBottom.name}


def build(name: str, symbol: str, *, htf: str = "15minute", ltf: str = "5minute",
          handoff: Handoff | None = None, **params: Any) -> MultiTimeframeStrategy:
    """Create one strategy instance for one symbol. Unknown names fail loudly."""
    if name not in BUY_STRATEGIES:
        raise KeyError(f"unknown BUY strategy {name!r}; registered: {sorted(BUY_STRATEGIES)}")
    if name in _NEEDS_HANDOFF:
        if handoff is None:
            raise ValueError(f"{name} needs a shared Handoff for symbol {symbol}")
        return BUY_STRATEGIES[name](symbol, htf=htf, ltf=ltf, handoff=handoff, **params)
    return BUY_STRATEGIES[name](symbol, htf=htf, ltf=ltf, **params)
